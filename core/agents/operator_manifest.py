"""Operator agents are plugin manifests stored on custom_agents (#4023).

The agent service owns the manifest's grammar: Python reads the few fields it
routes on (id, description, system, needs, skills) and leaves every structural
refusal to agent-serve's ``POST /plugins/check``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

import yaml

from core.agents.prompts import prompt_for_row

logger = logging.getLogger(__name__)

CHECK_ROUTE = "/plugins/check"
CHECK_TIMEOUT_S = 10

# What a legacy Agent Builder row converts to. A manifest with any other key
# says something the persona columns cannot, so the legacy shim leaves it alone.
LEGACY_KEYS = frozenset({"id", "description", "needs", "system", "conclusion"})


class ManifestRefused(Exception):
    """The manifest cannot be stored; ``str(exc)`` is the reason."""


class CheckUnavailable(Exception):
    """agent-serve could not be asked."""


# The one conversion: the migration fills every existing row with it, and the
# legacy POST/PATCH /agents/custom routes render their payload through it.
def manifest_from_row(row: Mapping[str, Any]) -> str:
    doc = {
        "id": row["id"],
        # parsePlugin requires a description; a row may have left it blank.
        "description": (row.get("description") or "").strip()
        or row.get("name")
        or row["id"],
        "needs": [str(t) for t in (row.get("recommended_tools") or [])],
        "system": prompt_for_row(row),
        "conclusion": "prose",
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=1000)


# Lenient: a stored manifest has passed the check, and an unreadable one reads as
# empty rather than taking the Agents list down with it.
def read_manifest(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _strings(value: Any) -> List[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def manifest_needs(doc: Mapping[str, Any]) -> List[str]:
    return _strings(doc.get("needs"))


def is_legacy_shaped(text: Optional[str]) -> bool:
    doc = read_manifest(text)
    return not doc or (set(doc) <= LEGACY_KEYS and doc.get("conclusion") == "prose")


# The skills a manifest names, from the same roots the console and the Python
# agents read. One it names and no root holds is left out: the check refuses it.
def manifest_skills(doc: Mapping[str, Any]) -> List[Dict[str, str]]:
    names = _strings(doc.get("skills"))
    if not names:
        return []
    from core.skills.skill_library import load_skills, skill_body, skill_roots

    found = {skill.name: skill for skill in load_skills(skill_roots())}
    return [
        {
            "name": found[name].name,
            "description": found[name].description,
            "body": skill_body(found[name]),
        }
        for name in dict.fromkeys(names)
        if name in found
    ]


# The parser a run loads with, so a manifest stored here is one a run accepts.
def check_manifest(text: str, skills: List[Dict[str, str]]) -> str:
    """The manifest's id, or ManifestRefused with agent-serve's reason."""
    import httpx

    from core.agents.projections import _headers, agent_route

    try:
        response = httpx.post(
            agent_route(CHECK_ROUTE),
            json={"manifest": text, "skills": skills},
            headers=_headers(),
            timeout=CHECK_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        raise CheckUnavailable(str(exc)) from exc
    if response.status_code == 422:
        try:
            reason = response.json().get("detail")
        except ValueError:
            reason = None
        raise ManifestRefused(reason or "the agent service refused the manifest")
    if response.status_code != 200:
        raise CheckUnavailable(f"the agent service answered {response.status_code}")
    return str(response.json().get("id") or "")


# The start request's operator fields for a definition that carries a manifest,
# else nothing. Skills are resolved now, so the run journals the bodies it used.
def operator_request(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    text = metadata.get("operator_manifest")
    if not text:
        return {}
    return {"manifest": text, "manifest_skills": manifest_skills(read_manifest(text))}
