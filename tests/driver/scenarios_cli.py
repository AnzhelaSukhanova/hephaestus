import io
import json
import os
import shlex
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch


def basic() -> None:
    pass


def validate() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    driver.validate_args(driver.cli_args)


def requires_correctness_preserving() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    with patch.object(driver.cli_args,
                      "only_correctness_preserving_transformations", False):
        driver.validate_args(driver.cli_args)


def requires_transformation_source() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    with patch.object(driver.cli_args, "transformations", None):
        driver.validate_args(driver.cli_args)


def defaults() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    workspace = Path.cwd()
    args = driver.cli_args
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
    assert not args.keep_everything
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
    assert driver.cfg.prob.crossmodule_probability == 0
    assert driver.STATS["Info"]["stop_cond_value"] == 1
    assert driver.STATS["Info"]["language"] == "kotlin"
    assert driver.STATS["totals"] == {"passed": 0, "failed": 0}
    assert "crossmodule" not in driver.STATS
    driver.validate_args(args)
    assert not Path(args.bugs).exists()


def configuration() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    assert driver.cfg.limits.max_depth == 4
    assert driver.cfg.limits.max_type_params == 2
    assert driver.cfg.limits.inline_default_depth == 3
    assert driver.cfg.limits.max_var_decls == 8
    assert driver.cfg.dis.use_site_variance is False
    assert driver.cfg.dis.use_site_contravariance is False
    assert driver.cfg.prob.bounded_type_parameters == 0.8
    assert driver.cfg.prob.parameterized_functions == 0.8
    assert driver.cfg.prob.reified_type_parameters == 0.8
    assert driver.cfg.prob.crossmodule_probability == 0
    driver.validate_args(driver.cli_args)


def configuration_flags() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    args = driver.cli_args
    assert driver.cfg.limits.max_depth == 0
    assert driver.cfg.limits.max_type_params == 0
    assert driver.cfg.limits.inline_default_depth == 0
    assert driver.cfg.limits.max_var_decls == 8
    assert driver.cfg.dis.use_site_variance is True
    assert driver.cfg.dis.use_site_contravariance is True
    assert driver.cfg.prob.bounded_type_parameters == 0
    assert driver.cfg.prob.parameterized_functions == 0
    assert driver.cfg.prob.reified_type_parameters == 0
    assert args.keep_everything and args.keep_all
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
    assert driver.STATS["Info"]["stop_cond"] == "timeout"
    assert driver.STATS["Info"]["stop_cond_value"] == 30
    assert driver.STATS["Info"]["transformation_types"] == ""
    driver.validate_args(args)


def non_kotlin() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    assert driver.cli_args.language != "kotlin"
    assert driver.cfg.prob.crossmodule_probability == 0
    assert "crossmodule" not in driver.STATS
    driver.validate_args(driver.cli_args)


def startup() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    events = Mock()
    command = driver.COMPILERS[driver.cli_args.language].get_compiler_version()
    with (
        patch.object(driver, "validate_args") as validation,
        patch.object(driver, "pre_process_args") as preprocessing,
        patch.object(driver, "run_command",
                     return_value=(True, " \n compiler version 1.2 \t\n")) as version,
        patch.object(driver, "setup_crossmodule_manager") as manager,
        patch.object(driver, "setup_live_jacoco") as jacoco,
        patch.object(driver, "run") as sequential,
        patch.object(driver, "run_parallel") as parallel,
    ):
        events.attach_mock(validation, "validate")
        events.attach_mock(preprocessing, "preprocess")
        events.attach_mock(version, "version")
        events.attach_mock(manager, "manager")
        events.attach_mock(jacoco, "jacoco")
        events.attach_mock(sequential, "run")
        events.attach_mock(parallel, "parallel")
        driver.main()
    dispatch = (call.run("compiler version 1.2")
                if driver.cli_args.debug or driver.cli_args.workers is None
                else call.parallel("compiler version 1.2"))
    assert events.mock_calls == [
        call.validate(driver.cli_args),
        call.preprocess(driver.cli_args),
        call.version(command),
        call.manager("compiler version 1.2"),
        call.jacoco(),
        dispatch,
    ]


def helpers() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    test_directory = Path(driver.cli_args.test_directory)
    assert Path(driver.get_generator_dir(7)) == (
        test_directory / "generator" / "iter_7")
    assert Path(driver.get_transformations_dir(7, 3)) == (
        test_directory / "transformations" / "iter_7" / "3")
    assert not test_directory.exists()
    with patch("src.args.mkdir", wraps=driver.utils.mkdir) as mkdir:
        driver.pre_process_args(driver.cli_args)
        bugs = Path(driver.cli_args.bugs)
        assert bugs.is_dir()
        marker = bugs / "keep.txt"
        marker.write_text("preserved", encoding="utf-8")
        driver.pre_process_args(driver.cli_args)
        mkdir.assert_called_once_with(driver.cli_args.bugs)
        assert marker.read_text(encoding="utf-8") == "preserved"

    def combine(value: int, *, suffix: str) -> str:
        return str(value) + suffix

    with patch.object(driver.time, "perf_counter", side_effect=[10.0, 10.25]) as clock:
        assert driver.timed(combine, 4, suffix="!") == ("4!", 0.25)
        assert clock.call_count == 2

    failure = ValueError("timed callable failed")

    def fail() -> None:
        raise failure

    with patch.object(driver.time, "perf_counter", return_value=10.0) as clock:
        try:
            driver.timed(fail)
        except ValueError as error:
            assert error is failure
        else:
            raise AssertionError("timed() swallowed the callable's error")
        clock.assert_called_once_with()


def logging_and_progress() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    args = driver.cli_args
    driver.STATS["totals"].update(passed=3, failed=2)
    output = io.StringIO()
    with redirect_stdout(output), patch.object(args, "iterations", 8):
        driver.print_msg()
    assert output.getvalue() == (
        "\033[2K\033[1GTest Programs Passed 3 / 8 ✔\t\t"
        "Test Programs Failed 2 / 8 ✘\r")
    output = io.StringIO()
    with redirect_stdout(output), patch.object(args, "iterations", 0):
        driver.print_msg()
    assert output.getvalue() == (
        "\033[2K\033[1GTest Programs Passed 3 / 5 ✔\t\t"
        "Test Programs Failed 2 / 5 ✘\r")

    log_file = Path(args.log_file)
    log_file.write_text("existing log\n", encoding="utf-8")
    output = io.StringIO()
    with redirect_stdout(output), patch.object(driver, "print_msg") as progress:
        driver.logging("compiler version")
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
    assert driver.STATS["Info"]["compiler"] == "compiler version"
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
    from tests.driver.support import load_driver

    driver = load_driver()
    workspace = Path.cwd()
    script = _child_script(workspace)
    child_cwd = workspace / "child cwd"
    child_cwd.mkdir()
    argument = "spaces, 'quotes', and $literal"
    command = _python_command(script, str(exit_code), argument)
    with patch.dict(os.environ, {"JAVA_OPTS": "-Xmx64m"}):
        assert driver.run_command(command, cwd=str(child_cwd)) == (
            exit_code == 0, _expected_output(child_cwd, argument))
        assert driver.run_command(command, get_stdout=False, cwd=str(child_cwd)) == (
            exit_code == 0, "")
        assert driver.run_command(command) == (
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
    assert driver.run_command(_python_command(script)) == (True, "")


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
    temporary_cwd = Path(driver.cli_args.test_directory) / "tmp"
    argument = "groovy argument"
    with patch.dict(os.environ, {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "JAVA_OPTS": "-Xmx64m",
    }):
        for exit_code in (0, 9):
            assert driver.run_command([
                "groovyc", str(exit_code), shlex.quote(argument),
            ]) == (exit_code == 0, _expected_output(temporary_cwd, argument))
            assert temporary_cwd.is_dir()
            assert Path.cwd() == workspace
        assert driver.run_command(
            ["groovyc", "0", shlex.quote(argument)], cwd=str(workspace),
        ) == (True, _expected_output(workspace, argument))
        assert os.environ["JAVA_OPTS"] == "-Xmx64m"
    assert Path.cwd() == workspace


def command_launch_errors() -> None:
    from tests.driver.support import load_driver

    driver = load_driver()
    command = [shlex.quote(sys.executable), "unused-script.py"]
    failure = subprocess.CalledProcessError(4, command, output=b"launch failed")
    with patch.object(driver.sp, "Popen", side_effect=failure) as popen:
        status, output = driver.run_command(command)
        assert status is False
        assert output is failure
        popen.assert_called_once()
    launch_error = OSError("controlled launch error")
    with patch.object(driver.sp, "Popen", side_effect=launch_error) as popen:
        try:
            driver.run_command(command)
        except OSError as error:
            assert error is launch_error
        else:
            raise AssertionError("run_command() swallowed an OSError")
        popen.assert_called_once()