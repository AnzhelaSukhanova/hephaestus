import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.driver.support import launch


def _launch(workspace: Path, scenario: str,
            arguments: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    return launch(f"tests.driver.scenarios_cli.{scenario}", workspace, arguments)


def _assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_error(result: subprocess.CompletedProcess[str], message: str,
                  returncode: int = 1) -> None:
    assert result.returncode == returncode, result.stdout + result.stderr
    assert message in result.stderr, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def _write_config(workspace: Path) -> Path:
    path = workspace / "overrides.json"
    path.write_text(json.dumps({
        "limits": {
            "max_depth": 4,
            "max_type_params": 2,
            "inline_default_depth": 3,
            "max_var_decls": 8,
        },
        "dis": {"use_site_variance": False, "use_site_contravariance": False},
        "prob": {
            "crossmodule_probability": 0,
            "bounded_type_parameters": 0.8,
            "parameterized_functions": 0.8,
            "reified_type_parameters": 0.8,
        },
    }), encoding="utf-8")
    return path


def test_package_exposes_driver_entrypoint() -> None:
    project = Path(__file__).resolve().parents[2]
    with (project / "pyproject.toml").open("rb") as source:
        metadata = tomllib.load(source)
    assert metadata["project"]["scripts"]["hephaestus"] == "hephaestus:main"
    assert "hephaestus" in metadata["tool"]["setuptools"]["py-modules"]


@pytest.mark.parametrize("argument", ["-h", "--help"])
def test_help_exits_before_running_driver(tmp_path: Path, argument: str) -> None:
    result = _launch(tmp_path, "basic", [argument])
    _assert_success(result)
    assert "usage:" in result.stdout
    for option in ("--generator-config", "--workers", "--keep-everything",
                   "--time-metrics", "--jacoco-lowerings", "--jacoco-always"):
        assert option in result.stdout
    assert not (tmp_path / "bugs").exists()
    assert not (tmp_path / "run.log").exists()


@pytest.mark.parametrize(("arguments", "message"), [
    (["--language", "rust"], "invalid choice: 'rust'"),
    (["--backend", "jvm"], "invalid choice: 'jvm'"),
    (["--transformation-types", "missing"], "invalid choice: 'missing'"),
    (["--workers", "many"], "invalid int value: 'many'"),
    (["--iterations", "many"], "invalid int value: 'many'"),
    (["--generator-config"], "expected one argument"),
    (["--jacoco-lowerings"], "expected at least one argument"),
    (["--unknown-driver-option"], "unrecognized arguments: --unknown-driver-option"),
])
def test_parser_rejects_invalid_arguments(
        tmp_path: Path, arguments: list[str], message: str) -> None:
    result = _launch(tmp_path, "basic", arguments)
    _assert_error(result, message, returncode=2)
    assert "usage:" in result.stderr
    assert not (tmp_path / "bugs").exists()


@pytest.mark.parametrize(("arguments", "message"), [
    (["--seconds", "3"], "you should only set --seconds or --iterations"),
    (["--transformation-schedule", "schedule.txt", "-t", "1"],
     "Options --transformation-schedule and --transformations are mutually exclusive"),
    (["--transformation-schedule", "missing.txt"],
     "You have to provide a valid file in --transformation-schedule"),
    (["--rerun", "--workers", "2"], "You cannot use -r option in parallel mode"),
    (["--rerun"], "The -r option only works with the option -k"),
    (["--rerun", "--keep-all"], "You cannot use -r option with the option --batch"),
    (["--examine"], "You cannot use --examine option without the --replay option"),
    (["--dump-ir", "--batch", "2"],
     "You cannot use --dump-ir option with the option --batch != 1"),
    (["--time-metrics", "--batch", "2"],
     "You cannot use --time-metrics with --batch != 1"),
    (["--jacoco-lowerings", "FunctionInlining"],
     "The --jacoco-lowerings option requires --dump-ir"),
    (["--jacoco-lowerings", "FunctionInlining", "--dump-ir"],
     "The --jacoco-lowerings option requires --keep-everything"),
    (["--jacoco-lowerings", "FunctionInlining", "--dump-ir", "--keep-everything",
      "--jacoco-always"],
     "Options --jacoco-lowerings and --jacoco-always are mutually exclusive"),
    (["--jacoco-always"], "The --jacoco-always option requires --keep-everything"),
])
def test_validation_rejects_incompatible_arguments(
        tmp_path: Path, arguments: list[str], message: str) -> None:
    _assert_error(_launch(tmp_path, "validate", arguments), message)
    assert not (tmp_path / "bugs").exists()


@pytest.mark.parametrize(("scenario", "message"), [
    ("requires_correctness_preserving", "Error: Kotlin requires -P (for now)"),
    ("requires_transformation_source",
     "You have to provide one of --transformation-schedule or --transformations."),
])
def test_validation_guards_hidden_by_launcher_defaults(
        tmp_path: Path, scenario: str, message: str) -> None:
    _assert_error(_launch(tmp_path, scenario), message)


def test_validation_rejects_existing_session(tmp_path: Path) -> None:
    session = tmp_path / "bugs" / "session"
    session.mkdir(parents=True)
    marker = session / "keep.txt"
    marker.write_text("existing session", encoding="utf-8")
    _assert_error(_launch(tmp_path, "validate"), "Error: --name session already exists")
    assert marker.read_text(encoding="utf-8") == "existing session"


@pytest.mark.parametrize("arguments", [
    [],
    ["--seconds", "3", "--iterations", "0"],
    ["--rerun", "--keep-all", "--batch", "0"],
    ["--examine", "--replay", "program.bin"],
    ["--dump-ir", "--keep-everything", "--jacoco-lowerings", "FunctionInlining"],
    ["--keep-everything", "--jacoco-always"],
    ["--time-metrics", "--disable-metrics"],
])
def test_validation_accepts_supported_combinations(
        tmp_path: Path, arguments: list[str]) -> None:
    _assert_success(_launch(tmp_path, "validate", arguments))


def test_validation_accepts_existing_transformation_schedule(tmp_path: Path) -> None:
    schedule = tmp_path / "schedule.txt"
    schedule.write_text("TypeErasure\n", encoding="utf-8")
    _assert_success(_launch(tmp_path, "validate", [
        "--transformation-schedule", str(schedule),
    ]))


@pytest.mark.parametrize(("arguments", "message"), [
    (["--language", "java", "--keep-all"],
     "cross-module generation is supported only for Kotlin"),
    ([], "cross-module generation requires --keep-all"),
    (["--keep-all", "--batch", "2"], "cross-module generation requires --batch 1"),
    (["--keep-all", "--replay", "program.bin"],
     "cross-module generation does not support --replay"),
    (["--keep-all", "--rerun"], "cross-module generation does not support --rerun"),
    (["--keep-all", "--dry-run"], "cross-module generation does not support --dry-run"),
])
def test_validation_rejects_crossmodule_conflicts(
        tmp_path: Path, arguments: list[str], message: str) -> None:
    config = tmp_path / "crossmodule.json"
    config.write_text('{"prob": {"crossmodule_probability": 0.5}}', encoding="utf-8")
    _assert_error(_launch(tmp_path, "validate", [
        "--generator-config", str(config), *arguments,
    ]), message)


def test_validation_accepts_crossmodule_with_preservation(tmp_path: Path) -> None:
    config = tmp_path / "crossmodule.json"
    config.write_text('{"prob": {"crossmodule_probability": 0.5}}', encoding="utf-8")
    _assert_success(_launch(tmp_path, "validate", [
        "--generator-config", str(config), "--keep-all",
    ]))


@pytest.mark.parametrize("contents", [
    "{not json",
    "[]",
    '{"unknown_option": 1}',
    '{"limits": {"max_depth": "deep"}}',
    '{"prob": {"class_type": {"regular": 1, "abstract": 1, "interface": 0}}}',
])
def test_invalid_generator_configuration_is_reported(
        tmp_path: Path, contents: str) -> None:
    config = tmp_path / "invalid.json"
    config.write_text(contents, encoding="utf-8")
    _assert_error(_launch(tmp_path, "basic", ["--generator-config", str(config)]),
                  f"Error loading --generator-config {config}:")


def test_missing_generator_configuration_is_reported(tmp_path: Path) -> None:
    config = tmp_path / "missing.json"
    _assert_error(_launch(tmp_path, "basic", ["--generator-config", str(config)]),
                  f"Error loading --generator-config {config}:")


def test_generator_configuration_overrides_defaults(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _assert_success(_launch(tmp_path, "configuration", ["--generator-config", str(config)]))


def test_cli_flags_override_configuration_and_populate_options(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _assert_success(_launch(tmp_path, "configuration_flags", [
        "--generator-config", str(config),
        "--max-depth", "0", "--max-type-params", "0", "--inline-default-depth", "0",
        "--disable-use-site-variance", "--disable-contravariance-use-site",
        "--disable-bounded-type-parameters", "--disable-parameterized-functions",
        "--disable-reified-type-parameters", "--keep-everything",
        "--time-metrics", "--disable-metrics", "--dump-ir", "--dry-run",
        "--log", "--print-stacktrace", "--cast-numbers", "--timeout", "17",
        "--seconds", "30", "--iterations", "0", "--backend", "js",
        "--transformation-types",
    ]))


@pytest.mark.parametrize("language", ["groovy", "java", "scala"])
def test_non_kotlin_disables_default_crossmodule_probability(
        tmp_path: Path, language: str) -> None:
    config = tmp_path / "empty.json"
    config.write_text("{}", encoding="utf-8")
    _assert_success(_launch(tmp_path, "non_kotlin", [
        "--language", language, "--generator-config", str(config),
    ]))


@pytest.mark.parametrize("arguments", [
    [],
    ["--debug"],
    ["--workers", "2"],
    ["--debug", "--workers", "2"],
    ["--language", "groovy", "--workers", "2"],
    ["--language", "java"],
    ["--language", "scala"],
])
def test_startup_order_version_stripping_and_dispatch(
        tmp_path: Path, arguments: list[str]) -> None:
    _assert_success(_launch(tmp_path, "startup", arguments))


@pytest.mark.parametrize("arguments", [
    [],
    ["--debug"],
    ["--iterations", "0"],
    ["--iterations", "0", "--seconds", "5"],
])
def test_logging_and_progress_branches(tmp_path: Path, arguments: list[str]) -> None:
    _assert_success(_launch(tmp_path, "logging_and_progress", arguments))


@pytest.mark.parametrize("scenario", [
    "defaults",
    "helpers",
    "command_success",
    "command_failure",
    "command_empty_output",
    "command_groovy",
    "command_launch_errors",
])
def test_driver_helpers_and_process_execution(tmp_path: Path, scenario: str) -> None:
    _assert_success(_launch(tmp_path, scenario))