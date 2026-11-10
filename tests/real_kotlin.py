"""Skip checks for tests that drive the installed ``master`` compiler.

Such tests carry the ``real_kotlin`` mark, so ``tests/conftest.py`` leaves the
real store in place for them.
"""
from __future__ import annotations

from collections.abc import Iterable

import pytest

from src.compilers.kotlin import KotlinCompiler, KotlinSettings
from src.tools.kotlin_compilers.layout import Layout


def master_problem(settings: Iterable[KotlinSettings]) -> str | None:
    """Why ``master`` cannot run every one of ``settings``, or ``None``."""
    layout = Layout()
    descriptor = layout.descriptor('master')
    if descriptor is None:
        return f'no installed master compiler in {layout.root}; run sync first'
    for item in settings:
        if item.executable is None and item.backend not in descriptor['backends']:
            return f'master has no {item.backend} backend'
        problem = KotlinCompiler('', settings=item).installation_problem()
        if problem is not None:
            return problem
    return None


def skip_without_master(settings: Iterable[KotlinSettings]) -> None:
    problem = master_problem(settings)
    if problem is not None:
        pytest.skip(problem)
