"""Draft a skill from plain English, for the skill drawer's form.

Nothing is written under a skills root: the analyst reads the draft in the form,
edits it, and the existing Save writes it and applies the library's rules.
"""

import asyncio
import re

from pydantic import BaseModel

from core.llm.oneshot import complete
from core.skills.skill_library import (
    _DESCRIPTION_MAX,
    _NAME_MAX,
    SkillError,
    load_skills,
    skill_roots,
)

_SYSTEM_PROMPT = (
    "You write skills for the Vigil SOC platform. A skill is a SKILL.md file an "
    "agent reads when its description matches the task in front of it."
)

_OUTPUT_RULES = f"""## Output
Return the SKILL.md file and nothing else, in exactly this shape:

---
name: <the name>
description: <the description>
---

1. <a step>
2. <a step>

The name has lowercase letters, digits and single hyphens, at most {_NAME_MAX} characters.
The description is one line saying when an agent should use this skill, at most {_DESCRIPTION_MAX} characters. Agents decide from this text alone, so write it for this skill.
The steps are a numbered Markdown list, one step on each line, without HTML.
The skill is this one file. Do not write or refer to scripts or other files."""


_FIELD_RE = re.compile(r"^(name|description):[ \t]*(\S.*)$", re.M)


class SkillDraft(BaseModel):
    name: str
    description: str
    body: str


def _parse_draft(raw: str) -> SkillDraft:
    """The model's reply as a draft for the form, or a ``SkillError`` saying why not."""
    # Line by line, not as YAML: local models drop the closing --- and leave a Windows
    # path or a colon unquoted, and the analyst reads and edits the result anyway.
    fields: dict[str, str] = {}
    end = 0
    for match in _FIELD_RE.finditer(raw):
        if match.group(1) in fields:
            break
        fields[match.group(1)] = match.group(2).strip().strip("\"'")
        end = match.end()
    body = raw[end:].strip().removeprefix("---").removesuffix("```").strip()
    body = body.removesuffix("---").strip()
    if not (fields.get("name") and fields.get("description") and body):
        raise SkillError(
            "The model did not return a skill with a name, a description and steps."
        )
    return SkillDraft(
        name=re.sub(r"[^a-z0-9]+", "-", fields["name"].lower()).strip("-")[:_NAME_MAX],
        description=fields["description"],
        body=body,
    )


async def generate_skill_draft(description: str) -> SkillDraft:
    """A draft of the skill ``description`` asks for.

    Raises ``SkillError`` for a reply that is not a draft; errors from the model
    call propagate.
    """
    skills = await asyncio.to_thread(load_skills, skill_roots())
    taken = ", ".join(s.name for s in skills)
    raw = await complete(
        message=(
            f"## What the skill is for\n{description.strip()}\n\n"
            f"## Names already taken\n{taken}\n\n"
            "The name must not be one of these.\n\n"
            f"{_OUTPUT_RULES}"
        ),
        system_prompt=_SYSTEM_PROMPT,
    )
    return _parse_draft(raw)
