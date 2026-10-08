#!/usr/bin/env python3
"""Live-model check a skill's author runs on the PR that adds or changes it.

Paste the printed summary into that PR. There is no scheduled run; the
deterministic gate is ``tests/unit/skills/test_library_gate.py``. For each
skill the system prompt is ``render_base_prompt`` for a role, a note that no
tool is callable, and the SKILL.md body inlined; each case's ``input`` is sent
as one user turn through ``LLMRouter.dispatch`` (Bifrost, any provider) and
graded by substring containment against ``expect``. Exit is 1 below 100
percent, 2 for an unknown ``--skill``, and 3 when the provider names a key
env var that is unset. A keyless provider (ollama) needs no key. A key set
with Bifrost unreachable is a real failure. ``--provider`` is required.

Usage::

    python scripts/skill_eval.py --provider anthropic
    python scripts/skill_eval.py --provider anthropic --skill evals-skill
    python scripts/skill_eval.py --provider gemini --model gemini-flash-latest
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_settings  # noqa: E402
from core.llm.router.router import LLMRouter, ProviderSpec  # noqa: E402
from core.skills.skill_eval import run_cases  # noqa: E402
from core.skills.skill_library import LIBRARY_ROOT, Skill, load_skills  # noqa: E402

# The env var a provider's key lives in; None for a keyless provider. Anything
# not listed follows the <PROVIDER>_API_KEY pattern Bifrost's config uses.
KEY_ENV: Dict[str, Optional[str]] = {"ollama": None}


def key_env_name(provider: str) -> Optional[str]:
    return KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")


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
    # One case at a time, as the CLI always has: a rate limit or a mid-run
    # failure behaves as before.
    results = await run_cases(
        router,
        provider,
        skill,
        role=role,
        root=root,
        max_tokens=max_tokens,
        sequential=True,
    )
    for r in results:
        if not r.passed:
            print(f"  FAIL {skill.name} / {r.name}: missing {r.missing}")
    passed = sum(r.passed for r in results)
    print(f"{skill.name}: {passed}/{len(results)} passed")
    return passed, len(results)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--skill", help="run only this skill (default: every skill under --root)"
    )
    parser.add_argument("--provider", required=True, help="Bifrost provider type")
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
        print(f"skill_eval: {key_name} not set", file=sys.stderr)
        return 3
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
