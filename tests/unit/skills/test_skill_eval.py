"""The containment grader and per-skill summary of ``scripts/skill_eval.py`` (#926).

``LLMRouter.dispatch`` is stubbed: nothing in ``tests/unit/`` reaches Bifrost.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from core.llm.router.router import LLMRouter

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
FIXTURE_LIBRARY = Path(__file__).resolve().parent / "fixtures" / "library"

_spec = importlib.util.spec_from_file_location(
    "skill_eval", REPO / "scripts" / "skill_eval.py"
)
skill_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill_eval)


def test_grade_reports_the_missing_strings_only():
    assert (
        skill_eval.grade("SEVERITY: high\nVERDICT: ESCALATE", ["high", "ESCALATE"])
        == []
    )
    assert skill_eval.grade("SEVERITY: low", ["high", "low"]) == ["high"]


def test_an_object_input_is_sent_as_indented_json():
    assert skill_eval.as_user_turn("hello") == "hello"
    assert skill_eval.as_user_turn({"id": "f-1"}) == '{\n  "id": "f-1"\n}'


def _stub_dispatch(monkeypatch, answer_for):
    seen = []

    async def dispatch(self, *, provider, messages, system_prompt=None, **kwargs):
        seen.append((provider, messages, system_prompt, kwargs))
        return {"content": answer_for(messages[0]["content"])}

    monkeypatch.setattr(LLMRouter, "dispatch", dispatch)
    return seen


def test_a_perfect_run_passes_every_case_and_exits_zero(monkeypatch, capsys):
    def answer(user_turn):
        if "finding-001" in user_turn:
            return "SEVERITY: high\nTECHNIQUES: T1059.001, T1027\nVERDICT: ESCALATE"
        if "finding-002" in user_turn:
            return "SEVERITY: medium\nTECHNIQUES: T1110.001\nVERDICT: MONITOR"
        return "NO-FINDING"

    seen = _stub_dispatch(monkeypatch, answer)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    code = skill_eval.asyncio.run(
        skill_eval.main(["--root", str(FIXTURE_LIBRARY), "--skill", "evals-skill"])
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "evals-skill: 3/3 passed" in out
    assert "skill_eval: 3/3 cases passed across 1 skill(s)" in out
    provider, messages, system_prompt, kwargs = seen[0]
    assert provider.provider_type == "anthropic"
    assert messages[0]["role"] == "user"
    # Base prompt for the role, then the SKILL.md body; no tools on the call.
    assert system_prompt.startswith("You are a SOC analyst")
    assert "VERDICT: ESCALATE" in system_prompt
    assert "tools" not in kwargs


def test_one_miss_is_named_and_the_run_exits_nonzero(monkeypatch, capsys):
    _stub_dispatch(monkeypatch, lambda turn: "NO-FINDING")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    code = skill_eval.asyncio.run(
        skill_eval.main(
            ["--root", str(FIXTURE_LIBRARY), "--provider", "gemini", "--model", "m"]
        )
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "evals-skill: 1/3 passed" in out
    assert (
        "FAIL evals-skill / high severity powershell finding escalates: missing" in out
    )
    assert "'SEVERITY: high'" in out


def test_without_a_key_the_run_is_skipped_with_exit_zero(monkeypatch, capsys):
    def boom(self, **kwargs):
        raise AssertionError("dispatch must not be called without a key")

    monkeypatch.setattr(LLMRouter, "dispatch", boom)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = skill_eval.asyncio.run(skill_eval.main(["--root", str(FIXTURE_LIBRARY)]))
    assert code == 0
    assert "ANTHROPIC_API_KEY not set; skipping" in capsys.readouterr().out


def test_an_unknown_skill_name_is_an_error(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    code = skill_eval.asyncio.run(
        skill_eval.main(["--root", str(FIXTURE_LIBRARY), "--skill", "nope"])
    )
    assert code == 2
    assert "no skill named 'nope'" in capsys.readouterr().err
