import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_program_result_keeps_dependency_inputs_out_of_stats():
    script = """
import sys
from typing import List, Optional

sys.argv = ["hephaestus.py", "--language", "kotlin"]

import hephaestus

result = hephaestus.ProgramRes(
    False, {}, direct_dependency_pid=7,
    dep_transitive_closure_klibs=["/published/d7.klib"])
assert result.direct_dependency_pid == 7
assert result.dep_transitive_closure_klibs == ["/published/d7.klib"]
assert "dependency" not in result.stats
assert hephaestus.ProgramRes.__annotations__["direct_dependency_pid"] == Optional[int]
assert (hephaestus.ProgramRes.__annotations__["dep_transitive_closure_klibs"]
        == Optional[List[str]])
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=True,
    )