"""Each Compose live suite owns its project and refuses one it did not create.

No Docker: constructing a suite's Stack is the first thing its fixture does,
and that is what refuses. A subprocess here means the guard ran too late.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.medic_compose.compose import ComposeProjectRefused
from tests.medic_compose.test_compose_live import PROJECT_ENV as S6_ENV
from tests.medic_compose.test_compose_live import Stack as S6Stack
from tests.medic_compose.test_enable_live import PROJECT_ENV as V1_ENV
from tests.medic_compose.test_enable_live import Stack as V1Stack
from tests.medic_compose.test_last_seen_live import PROJECT_ENV as S9_ENV
from tests.medic_compose.test_last_seen_live import Stack as S9Stack
from tests.medic_compose.test_start_path_live import PROJECT_ENV as S11_ENV
from tests.medic_compose.test_start_path_live import Stack as S11Stack

# One variable family, the defaults these suites already had.
SUITES = (
    ("s6", S6Stack, S6_ENV, "medic-s6"),
    ("v1", V1Stack, V1_ENV, "medic-v1"),
    ("s11", S11Stack, S11_ENV, "medic-s11"),
    ("s9", S9Stack, S9_ENV, "medic-s9"),
)

pytestmark = pytest.mark.unit


def _no_subprocess(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError(f"subprocess ran: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "run", boom)


@pytest.mark.parametrize(
    "stack_cls, env_var",
    [(stack, env) for _, stack, env, _ in SUITES],
    ids=[name for name, *_ in SUITES],
)
@pytest.mark.parametrize("bad", ["deeptempo", "docker"])
def test_suite_refuses_before_any_subprocess(
    monkeypatch, tmp_path, stack_cls, env_var, bad
):
    _no_subprocess(monkeypatch)
    monkeypatch.setenv(env_var, bad)
    with pytest.raises(ComposeProjectRefused):
        stack_cls(tmp_path)


def test_unset_variables_keep_each_suites_default(monkeypatch, tmp_path):
    """The old shared knobs must not point every suite at one project."""
    _no_subprocess(monkeypatch)
    monkeypatch.setenv("VIGIL_MEDIC_COMPOSE_PROJECT", "medic-shared")
    monkeypatch.setenv("VIGIL_MEDIC_LAST_SEEN_PROJECT", "medic-shared")
    for _, _, env_var, _ in SUITES:
        monkeypatch.delenv(env_var, raising=False)
    assert [stack(tmp_path).project for _, stack, _, _ in SUITES] == [
        default for *_, default in SUITES
    ]
