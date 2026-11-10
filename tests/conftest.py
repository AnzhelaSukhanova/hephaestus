from __future__ import annotations

from pathlib import Path

import pytest

from tests.kotlin_compilers_fakes import fake_master


REAL_KOTLIN = 'real_kotlin'


def pytest_configure(config) -> None:
    config.addinivalue_line(
        'markers', f'{REAL_KOTLIN}: drives the installed compiler store instead of a fake master')


@pytest.fixture(autouse=True)
def master(request, tmp_path_factory, monkeypatch) -> Path | None:
    """The resolved build behind this test's fake ``master``.

    Keeps tests off the developer's ``~/kotlin-compilers``; tests marked
    ``real_kotlin`` see the real store instead and get ``None``.
    """
    if request.node.get_closest_marker(REAL_KOTLIN):
        return None
    return fake_master(tmp_path_factory.mktemp('kotlin-compilers'), monkeypatch)
