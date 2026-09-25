#!/usr/bin/env python3
"""Run every bundled skill's eval cases against a live model (epic #882, #926).

Nightly harness, not a PR gate: the deterministic gate is
``tests/unit/skills/test_library_gate.py``. For each skill the system prompt is
``render_base_prompt`` for a role, a note that no tool is callable, and the
SKILL.md body inlined; each case's ``input`` is sent as one user turn through
``LLMRouter.dispatch`` (Bifrost, any provider) and graded by substring
containment against ``expect``. Exit is nonzero below
100 percent. Without a provider key in the environment the run is skipped with
exit 0 so a nightly without secrets stays green; a key with Bifrost unreachable
is a real failure.

Usage::

    python scripts/skill_eval.py                       # every bundled skill
    python scripts/skill_eval.py --skill evals-skill   # one skill
    python scripts/skill_eval.py --provider gemini --model gemini-flash-latest
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agents.prompts import render_base_prompt  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.llm.router.router import LLMRouter, ProviderSpec  # noqa: E402
from core.skills.skill_library import (  # noqa: E402
    LIBRARY_ROOT,
    Skill,
    as_user_turn,
    load_skills,
    read_skill,
)

# The env var a provider's key lives in; None for a keyless provider. Anything
# not listed follows the <PROVIDER>_API_KEY pattern Bifrost's config uses.
KEY_ENV: Dict[str, Optional[str]] = {"ollama": None}


def key_env_name(provider: str) -> Optional[str]:
    return KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")


def grade(content: str, expect: Sequence[str]) -> List[str]:
    """The expected strings missing from the answer; empty means the case passed."""
    return [s for s in expect if s not in content]


def load_cases(skill: Skill) -> List[Dict[str, Any]]:
    return json.loads((skill.path / "evals" / "cases.json").read_text(encoding="utf-8"))


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


async def run_skill(
    router: LLMRouter,
    provider: ProviderSpec,
    skill: Skill,
    *,
    role: str,
    root: Path,
    max_tokens: int,
) -> Tuple[int, int]:
    """Run one skill's cases; returns (passed, total) and prints each failure."""
    system_prompt = system_prompt_for(skill, role, root)
    cases = load_cases(skill)
    passed = 0
    for case in cases:
        result = await router.dispatch(
            provider=provider,
            messages=[{"role": "user", "content": as_user_turn(case["input"])}],
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        missing = grade(result.get("content") or "", case["expect"])
        if missing:
            print(f"  FAIL {skill.name} / {case['name']}: missing {missing}")
        else:
            passed += 1
    print(f"{skill.name}: {passed}/{len(cases)} passed")
    return passed, len(cases)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--skill", help="run only this skill (default: every skill under --root)"
    )
    parser.add_argument("--provider", default="anthropic", help="Bifrost provider type")
    parser.add_argument("--model", help="model id (default: Settings.default_model)")
    parser.add_argument(
        "--role", default="analyst", help="role rendered into the base prompt"
    )
    parser.add_argument(
        "--root", type=Path, default=LIBRARY_ROOT, help="skills library root"
    )
    parser.add_argument(
        "--max-tokens", type=int, default=1024, help="answer budget per case"
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    # A mistyped skill name is an error even on a machine with no key.
    skills = load_skills([args.root])
    if args.skill:
        skills = [s for s in skills if s.name == args.skill]
        if not skills:
            print(
                f"skill_eval: no skill named {args.skill!r} under {args.root}",
                file=sys.stderr,
            )
            return 2
    key_name = key_env_name(args.provider)
    if key_name and not os.environ.get(key_name):
        print(f"skill_eval: {key_name} not set; skipping the model run")
        return 0
    if not skills:
        print(f"skill_eval: no skills under {args.root}; nothing to run")
        return 0

    provider = ProviderSpec(
        provider_id="skill-eval",
        provider_type=args.provider,
        base_url=None,
        api_key_ref=None,
        default_model=args.model or get_settings().default_model,
        config={},
    )
    router = LLMRouter()
    passed = total = 0
    for skill in skills:
        p, t = await run_skill(
            router,
            provider,
            skill,
            role=args.role,
            root=args.root,
            max_tokens=args.max_tokens,
        )
        passed += p
        total += t
    print(f"skill_eval: {passed}/{total} cases passed across {len(skills)} skill(s)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
