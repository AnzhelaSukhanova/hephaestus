import importlib
import re
import sys

import pytest

from src.compilers.base import BaseCompiler
from src.compilers.groovy import GroovyCompiler
from src.compilers.java import JavaCompiler
from src.compilers.scala import ScalaCompiler


def _load_kotlin_compiler(monkeypatch):
    with monkeypatch.context() as patcher:
        patcher.setattr(sys, "argv", ["hephaestus.py"])
        return importlib.import_module("src.compilers.kotlin")


class SyntheticCompiler(BaseCompiler[None]):
    ERROR_REGEX = re.compile(r"(?m)^([^:\n]+):(\d+): (.*)$")
    CRASH_REGEX = re.compile(r"CRASH")

    def get_filename(self, match: tuple[str, ...]) -> str:
        return match[0]

    def get_error_msg(self, match: tuple[str, ...]) -> str:
        return f"{match[1]}: {match[2]}"


def test_base_compiler_diagnostics_filters_and_enrichment_defaults():
    compiler = SyntheticCompiler("source", {r"ignore\.kt:\d+: [^\n]+\n?"},
                                 settings=None)

    failed, matches = compiler.analyze_compiler_output(
        "ignore.kt:1: filtered\nfile.kt:2: broken\nfile.kt:3: missing")

    assert failed == {"file.kt": ["2: broken", "3: missing"]}
    assert matches == [("file.kt", "2", "broken"),
                       ("file.kt", "3", "missing")]
    assert compiler.crash_msg is None
    assert compiler.get_error_enrichment_cmds("file.kt") == {}
    assert compiler.analyze_error_enrichment_output("file.kt", {}) == {}


def test_base_compiler_crash_returns_no_diagnostics():
    compiler = SyntheticCompiler("source", settings=None)
    output = "CRASH\nfile.kt:2: broken"

    assert compiler.analyze_compiler_output(output) == (None, [])
    assert compiler.crash_msg == output


@pytest.mark.parametrize(
    "compiler_cls, diagnostic, filename, message, match, crash",
    [
        ("kotlin", "src/Main.kt:4:5: error: nope\n", "src/Main.kt",
         "4:5: nope", ("src/Main.kt", "4", "5", "nope"),
         "org.jetbrains.kotlin.Crash\ntrace"),
        (JavaCompiler, "src/Main.java:7: error: nope\n", "src/Main.java",
         "7: error: nope", ("src/Main.java", "7: error: nope", ""),
         "java.lang.Error\ntrace"),
        (GroovyCompiler, "src/Main.groovy:broken\n\n", "src/Main.groovy",
         "broken", ("src/Main.groovy", "broken"),
         "at org.codehaus.groovy.X"),
        (ScalaCompiler,
         "-- [E1] Error: src/Main.scala:4:5 -------\nbroken\n----",
         "src/Main.scala", "broken\n", ("src/Main.scala", "broken\n"),
         "at dotty.X"),
    ],
)
def test_language_compiler_diagnostics_and_crash(
        compiler_cls, diagnostic, filename, message, match, crash,
        monkeypatch):
    if compiler_cls == "kotlin":
        compiler_cls = _load_kotlin_compiler(monkeypatch).KotlinCompiler
    compiler = compiler_cls("src")

    assert compiler.analyze_compiler_output(diagnostic) == (
        {filename: [message]}, [match])
    assert compiler.crash_msg is None
    assert compiler.analyze_compiler_output(crash) == (None, [])
    assert compiler.crash_msg == crash


def test_groovy_stack_overflow_without_diagnostic_is_crash():
    compiler = GroovyCompiler("src")
    output = "java.lang.StackOverflowError\n stack trace"

    assert compiler.analyze_compiler_output(output) == (None, [])
    assert compiler.crash_msg == output


def test_kotlin_phase_enrichment_and_commands(monkeypatch):
    kotlin_module = _load_kotlin_compiler(monkeypatch)
    compiler = kotlin_module.KotlinCompiler("src")
    kotlin_command = kotlin_module.compiler
    is_native = kotlin_module.is_native
    from src.args import args as cli_args
    expected_command = ([kotlin_command, "src", "-produce", "library", "-o",
                         "src", "-nowarn", "-Xklib-ir-inliner=full"]
                        if is_native else
                        [kotlin_command, "src", "-ir-output-dir", "src",
                         "-ir-output-name", "src", "-libraries",
                         compiler._stdlib(), "-nowarn",
                         "-Xklib-ir-inliner=full"])
    dump_flags = (["-Xphases-to-dump=" + compiler.IR_MODIFYING_INLINER_PHASES,
                   "-Xdump-directory=src/ir"] if cli_args.dump_ir else [])

    assert compiler.get_compiler_cmd() == expected_command + dump_flags
    assert compiler.get_error_enrichment_cmds("Main.kt") == {
        "xprofile-phases": compiler._get_compiler_cmd("Main.kt") +
        ["-Xprofile-phases"]}
    assert compiler.analyze_error_enrichment_output(
        "Main.kt", {"xprofile-phases": "Lowering: 8 msec"}) == {
        "error_phase": "backend"}
    assert compiler.analyze_error_enrichment_output("Main.kt", {}) == {
        "error_phase": "frontend"}


@pytest.mark.parametrize("compiler_cls, command", [
    (JavaCompiler, ["javac", "-nowarn", "src/*/*.java"]),
    (GroovyCompiler, ["groovyc-l", "--compile-static", "src/*/*.groovy"]),
    (ScalaCompiler, ["scalac", "-color", "never", "-nowarn",
                     "src/*/*.scala"]),
])
def test_language_compiler_command_is_unchanged(compiler_cls, command):
    assert compiler_cls("src").get_compiler_cmd() == command