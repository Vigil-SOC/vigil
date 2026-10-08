"""Grade a skill against its ``evals/cases.json`` through a live model.

Shared by ``scripts/skill_eval.py`` (the author's CLI check) and
``POST /api/skills/{name}/test`` (the skill drawer). For each skill the system
prompt is ``render_base_prompt`` for a role, a note that no tool is callable,
and the SKILL.md body inlined; each case's ``input`` is sent as one user turn
through ``LLMRouter.dispatch`` and graded by substring containment against
``expect``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.agents.prompts import render_base_prompt
from core.llm.router.router import LLMRouter, ProviderSpec
from core.skills.skill_library import Skill, as_user_turn, read_skill


@dataclass(frozen=True)
class CaseResult:
    name: str
    missing: List[str]

    @property
    def passed(self) -> bool:
        return not self.missing


def grade(content: str, expect: Sequence[str]) -> List[str]:
    """The expected strings missing from the answer; empty means the case passed."""
    return [s for s in expect if s not in content]


def cases_path(skill: Skill) -> Path:
    return skill.path / "evals" / "cases.json"


def has_cases(skill: Skill) -> bool:
    return cases_path(skill).is_file()


def load_cases(skill: Skill) -> List[Dict[str, Any]]:
    """The skill's cases; ``ValueError`` (or ``OSError``) when the file is not a list of cases."""
    cases = json.loads(cases_path(skill).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not all(
        isinstance(c, dict)
        and isinstance(c.get("name"), str)
        and "input" in c
        and isinstance(c.get("expect"), list)
        and all(isinstance(s, str) for s in c["expect"])
        for c in cases
    ):
        raise ValueError("cases.json is not a list of {name, input, expect: [str]}")
    return cases


# The eval declares no tools, so the base prompt is rendered without the
# read_skill grant and the body is inlined with a note saying so: a model told
# to call a tool it does not hold answers with no content at all (Gemini
# returns MALFORMED_FUNCTION_CALL), and the base prompt's static tool list
# still tempts it to stop mid-turn on a lookup.
_INLINE_NOTE = (
    "The SKILL.md body of `{name}` follows, already read for you. No tool is "
    "callable in this turn: answer in full from the input alone, following "
    "the skill.\n\n"
)


def system_prompt_for(skill: Skill, role: str, root: Path) -> str:
    body = read_skill(skill.name, roots=[root])["content"]
    return (
        render_base_prompt(role, tools=())
        + "\n\n"
        + _INLINE_NOTE.format(name=skill.name)
        + body
    )


async def run_cases(
    router: LLMRouter,
    provider: ProviderSpec,
    skill: Skill,
    *,
    role: str,
    root: Path,
    max_tokens: int,
    model: Optional[str] = None,
    cases: Optional[List[Dict[str, Any]]] = None,
    sequential: bool = False,
) -> List[CaseResult]:
    """Run a skill's cases (default: its cases.json), concurrently unless ``sequential``.

    A dispatch failure propagates: it is not a failed case.
    """
    system_prompt = system_prompt_for(skill, role, root)

    async def one(case: Dict[str, Any]) -> CaseResult:
        result = await router.dispatch(
            provider=provider,
            messages=[{"role": "user", "content": as_user_turn(case["input"])}],
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
        )
        return CaseResult(
            case["name"], grade(result.get("content") or "", case["expect"])
        )

    if cases is None:
        cases = load_cases(skill)
    if sequential:
        return [await one(c) for c in cases]
    return list(await asyncio.gather(*(one(c) for c in cases)))
