"""Operator agents are plugin manifests stored on custom_agents (#4023).

The agent service owns the manifest's grammar: Python reads the few fields it
routes on (id, description, system, needs, skills) and leaves every structural
refusal to agent-serve's ``POST /plugins/check``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

import yaml

logger = logging.getLogger(__name__)

CHECK_ROUTE = "/plugins/check"
CHECK_TIMEOUT_S = 10


class ManifestRefused(Exception):
    """The manifest cannot be stored; ``str(exc)`` is the reason."""


class CheckUnavailable(Exception):
    """agent-serve could not be asked."""


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


class _BlockDumper(yaml.SafeDumper):
    """Multi-line strings as | blocks, so a rendered prompt stays readable."""


_BlockDumper.add_representer(
    str,
    lambda dumper, value: dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
    ),
)


# The manifest a run loads: system rendered (core.agents.prompts.render_system),
# the rest as stored. Parsed and dumped rather than substituted in the text, since
# the $memory and $skills blocks are multi-line. Text with nothing to fill is
# returned as stored, comments and all.
def rendered_manifest(text: str) -> str:
    from core.agents.prompts import render_system

    doc = read_manifest(text)
    system = doc.get("system")
    if not isinstance(system, str):
        return text
    rendered = render_system(system, manifest_needs(doc))
    if rendered == system:
        return text
    return yaml.dump(
        {**doc, "system": rendered},
        Dumper=_BlockDumper,
        sort_keys=False,
        allow_unicode=True,
        width=1000,
    )


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


# Both checks a manifest passes before it is stored or shown as a draft: the
# parser a run loads with, then needs this deployment can bind. Returns its id.
def vet_manifest(text: str, registry: Any = None) -> str:
    from core.workflows.playbook_resolver import unknown_needs

    doc = read_manifest(text)
    checked = check_manifest(text, manifest_skills(doc))
    missing = unknown_needs(manifest_needs(doc), registry)
    if missing:
        raise ManifestRefused(
            f"needs {', '.join(missing)}, which is neither a capability nor a "
            "tool this deployment carries"
        )
    return checked


# The start request's operator fields for a definition that carries a manifest,
# else nothing. The prompt and skills are resolved now, so the run journals the
# text and bodies it used; the stored manifest keeps its placeholders.
def operator_request(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    text = metadata.get("operator_manifest")
    if not text:
        return {}
    return {
        "manifest": rendered_manifest(text),
        "manifest_skills": manifest_skills(read_manifest(text)),
    }
